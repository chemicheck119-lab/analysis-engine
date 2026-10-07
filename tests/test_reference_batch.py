"""Synthetic local reference fixtures; no API availability or real field claims."""

import shutil
import sqlite3
from pathlib import Path

import pytest

from chemiguard119 import reference_batch as batch
from chemiguard119.kosha_client import KoshaMsdsClient
from chemiguard119.resolver import load_resolver, resolve_substance
from chemiguard119.retrieval import load_retriever, search_evidence
from test_preprocessing import (
    _make_fixture,
    _patch_expected_counts,
    _write_csv,
    KOSHA_COLUMNS,
)
from test_kosha_client import SEARCH_XML, _detail_xml


@pytest.fixture
def setup(tmp_path, monkeypatch):
    _patch_expected_counts(monkeypatch)
    data, config, _ = _make_fixture(tmp_path)
    original_config = Path(__file__).parents[1] / "config"
    for path in original_config.iterdir():
        if path.is_file() and not (config / path.name).exists():
            shutil.copyfile(path, config / path.name)
    incoming = tmp_path / "fixture.csv"
    source = data / batch.preprocessing.SOURCE_FILES["kosha"]
    rows = batch.rows_from(source)
    _write_csv(incoming, KOSHA_COLUMNS, rows[:2])
    root = tmp_path / "batch"
    spec = {
        "baseline_dir": str(data),
        "config_dir": str(config),
        "fixture_csv": str(incoming),
        "cas_numbers": ["7647-01-0", "7681-52-9"],
        "max_excluded_fraction": 0.8,
        "count_change_threshold": 0.25,
    }
    return root, spec, incoming, rows


def staged(root, spec, run_id="first", fault=None):
    p = batch.Pipeline(root, run_id)
    p.initialize(spec)
    for step in batch.STEPS[:5]:
        p.execute(step, fault=fault)
    return p


def activate(p, fault=None):
    state = p.state()
    batch.approve(
        p.root,
        state["version"],
        "synthetic-test-reviewer",
        "Synthetic fixture mechanics only; not safety/production approval",
        True,
    )
    p.execute("activate", fault=fault)
    assert p.finish()["status"] == "SUCCESS"
    return state["version"]


def evidence(root):
    paths = batch.active_runtime_paths(root)
    with sqlite3.connect(paths["db_path"]) as db:
        return db.execute(
            "SELECT evidence_id, body FROM evidence ORDER BY evidence_id"
        ).fetchall()


def test_end_to_end_and_same_version_reexecution_no_duplicates(setup):
    root, spec, _, _ = setup
    p = staged(root, spec)
    version = activate(p)
    before = evidence(root)
    paths = batch.active_runtime_paths(root)
    resolver = load_resolver(paths["resolver_model_path"])
    resolution = resolve_substance("7647-01-0", resolver)
    assert resolution["status"] == "EXACT_IDENTIFIER_MATCH"
    assert resolution["candidates"][0]["cas_number"] == "7647-01-0"
    retriever = load_retriever(paths["retriever_model_path"])
    assert {row["evidence_id"] for row in retriever["rows"]} == {
        row[0] for row in before
    }
    retrieval = search_evidence(
        "염화수소", paths["db_path"], retriever, cas_hint="7647-01-0"
    )
    assert retrieval["status"] == "COMPLETED"
    assert retrieval["results"]
    assert all(row["cas_number"] == "7647-01-0" for row in retrieval["results"])
    second = staged(root, spec, "second")
    assert second.state()["version"] == version
    second.execute("activate")
    assert second.finish()["status"] == "SUCCESS"
    assert evidence(root) == before
    assert len(before) == len({row[0] for row in before})
    counts = second.state()["counts"]
    assert counts == {
        "input": 2,
        "normal": 2,
        "excluded": 0,
        "stored": 2,
        "baseline": 4,
        "reused": 2,
        "added": 0,
        "merged": 4,
        "baseline_detail_excluded": 2,
    }


def test_missing_invalid_duplicate_and_parse_shape_exclusions(setup):
    root, spec, incoming, rows = setup
    invalid = [
        dict(rows[0], 레코드ID="missing-cas", CAS번호=""),
        dict(rows[0], 레코드ID="missing-name", 화학물질명_국문=""),
        dict(rows[0], 레코드ID="bad-cas", CAS번호="123-45-6"),
        rows[0],
    ]
    _write_csv(incoming, KOSHA_COLUMNS, rows[:2] + invalid)
    # A nonrectangular record is counted and excluded, not silently lost.
    with incoming.open("a") as handle:
        handle.write("only,three,columns\n")
    p = staged(root, spec)
    state = p.state()
    assert state["counts"]["input"] == 7
    assert state["counts"]["normal"] == 2
    assert state["counts"]["excluded"] == 5
    reasons = state["exclusion_reasons"]
    for reason in (
        "CAS_MISSING",
        "REQUIRED_MISSING:화학물질명_국문",
        "CAS_INVALID_FORMAT_OR_CHECKSUM",
        "DUPLICATE_RECORD_ID",
        "PARSE_ROW_SHAPE",
    ):
        assert reasons[reason] >= 1
    activate(p)
    assert len(evidence(root)) == 4  # two KOSHA + two CAMEO


def test_unreviewed_and_stale_review_cannot_activate(setup):
    root, spec, _, _ = setup
    p = staged(root, spec)
    with pytest.raises(batch.BatchError, match="HUMAN_REVIEW_REQUIRED"):
        p.execute("activate")
    assert batch.current(root) is None
    assert p.finish()["status"] == "FAILED"
    v = p.state()["version"]
    batch.approve(root, v, "test", "synthetic review")
    path = root / "versions" / v / "incoming.csv"
    path.write_text("tampered")
    with pytest.raises(batch.BatchError, match="BUNDLE_HASH_MISMATCH"):
        p.execute("activate")
    assert batch.current(root) is None


def test_staging_and_activation_failures_preserve_previous_version_and_recover(setup):
    root, spec, incoming, rows = setup
    first = staged(root, spec)
    old = activate(first)
    previous = evidence(root)
    _write_csv(
        incoming,
        KOSHA_COLUMNS,
        rows[:2] + [dict(rows[0], 레코드ID="new-id", 상세내용="합성 추가 내용")],
    )
    p = batch.Pipeline(root, "stage-fail")
    p.initialize(spec)
    for step in batch.STEPS[:4]:
        p.execute(step)
    with pytest.raises(batch.BatchError, match="INJECTED_STAGING_FAILURE"):
        p.execute("stage", fault="stage")
    assert evidence(root) == previous
    assert batch.current(root)["version"] == old
    p.execute("stage")  # Remove incomplete staging and build again.
    new = p.state()["version"]
    batch.approve(root, new, "test", "synthetic fixture", True)
    with pytest.raises(batch.BatchError, match="INJECTED_ACTIVATION_FAILURE"):
        p.execute("activate", fault="activate")
    assert evidence(root) == previous
    p.execute("activate")
    p.finish()
    assert len(evidence(root)) == len(previous) + 1
    batch.rollback(root, old)
    assert evidence(root) == previous
    assert batch.current(root)["version"] == old
    # Archived version replay is independent of original source availability.
    replay_spec = dict(spec, reprocess_version=new)
    replay_spec.pop("fixture_csv")
    replay = staged(root, replay_spec, "replay")
    assert replay.state()["version"] == new
    replay.execute("activate")
    replay.finish()
    assert len(evidence(root)) == len(previous) + 1


def test_concurrent_and_aba_activation_is_blocked(setup):
    root, spec, incoming, rows = setup
    first = staged(root, spec)
    old = activate(first)
    _write_csv(
        incoming,
        KOSHA_COLUMNS,
        rows[:2] + [dict(rows[0], 레코드ID="next", 상세내용="합성 변경")],
    )
    stale = staged(root, spec, "stale")
    _write_csv(
        incoming,
        KOSHA_COLUMNS,
        rows[:2] + [dict(rows[0], 레코드ID="other", 상세내용="다른 합성 변경")],
    )
    other = staged(root, spec, "other")
    activate(other)
    batch.rollback(root, old)
    v = stale.state()["version"]
    batch.approve(root, v, "test", "fixture", True)
    with pytest.raises(
        batch.BatchError, match="CONCURRENT_ACTIVATION_BASELINE_CHANGED"
    ):
        stale.execute("activate")
    assert batch.current(root)["version"] == old


def test_network_retry_success_and_exhaustion_preserve_service(setup):
    root, spec, _, _ = setup
    first = staged(root, spec)
    activate(first)
    previous = evidence(root)
    api_spec = dict(spec, cas_numbers=["67-56-1"])
    api_spec.pop("fixture_csv")
    calls = []

    def fetch(url, timeout):
        calls.append(url)
        if len(calls) == 1:
            raise TimeoutError("secret URL must not appear in batch logs")
        if "/getChemList001?" in url:
            return SEARCH_XML
        return _detail_xml(int(url.split("getChemDetail")[1][:2]))

    client = KoshaMsdsClient("test-secret", fetch_xml=fetch, sleep=lambda _: None)
    p = batch.Pipeline(root, "transient")
    p.initialize(api_spec)
    p.execute("collect", client=client)
    assert len(calls) == 18  # one retry + list + 16 sections
    assert p.state()["collection"]["request_count"] == 18
    for step in batch.STEPS[1:5]:
        p.execute(step)
    activate(p)
    assert len(evidence(root)) == len(previous) + 16
    previous = evidence(root)

    def fail(*_):
        raise TimeoutError("test-secret")

    client = KoshaMsdsClient("test-secret", fetch_xml=fail, sleep=lambda _: None)
    failed = batch.Pipeline(root, "exhausted")
    failed.initialize(api_spec)
    with pytest.raises(batch.BatchError, match="KOSHA_NETWORK_ERROR"):
        failed.execute("collect", client=client)
    assert client.request_count == 3
    assert failed.finish()["status"] == "FAILED"
    assert evidence(root) == previous
    assert "test-secret" not in failed.state_path.read_text()


def test_count_drift_requires_explicit_review_and_validation_not_retried(setup):
    root, spec, incoming, rows = setup
    activate(staged(root, spec))
    _write_csv(
        incoming,
        KOSHA_COLUMNS,
        rows[:2] + [dict(rows[0], 레코드ID=f"added-{n}", 詳細="") for n in []],
    )
    expanded = rows[:2] + [
        dict(rows[0], 레코드ID=f"added-{n}", 상세내용=f"합성 정보 {n}")
        for n in range(3)
    ]
    _write_csv(incoming, KOSHA_COLUMNS, expanded)
    p = staged(root, spec, "drift")
    assert p.state()["count_change"]["relative_change"] == 1.5
    batch.approve(root, p.state()["version"], "test", "fixture", False)
    with pytest.raises(batch.BatchError, match="COUNT_CHANGE_REVIEW_REQUIRED"):
        p.execute("activate")
    # Invalid file has deterministic failure and cannot reach staging.
    incoming.write_text('bad,header\n"unterminated')
    failed = batch.Pipeline(root, "bad-file")
    failed.initialize(spec)
    failed.execute("collect")
    failed.execute("archive")
    with pytest.raises(batch.BatchError, match="REQUIRED_COLUMNS_MISSING"):
        failed.execute("normalize")
    assert "stage" not in failed.state()["done"]


def test_old_baseline_cannot_drop_previous_successful_additions(setup):
    root, spec, incoming, rows = setup
    first = staged(root, spec)
    activate(first)
    _write_csv(
        incoming,
        KOSHA_COLUMNS,
        rows[:2] + [dict(rows[0], 레코드ID="kept", 상세내용="합성 보존 근거")],
    )
    second = staged(root, spec, "addition")
    second_version = activate(second)
    previous = evidence(root)
    _write_csv(
        incoming,
        KOSHA_COLUMNS,
        rows[:2] + [dict(rows[0], 레코드ID="different", 상세내용="다른 합성 근거")],
    )
    bad = staged(root, spec, "old-baseline")
    batch.approve(root, bad.state()["version"], "test", "fixture", True)
    with pytest.raises(batch.BatchError, match="BASELINE_WOULD_LOSE_ACTIVE_EVIDENCE"):
        bad.execute("activate")
    assert evidence(root) == previous
    # Explicitly base next run on the active source snapshot; additions survive.
    good_spec = dict(
        spec, baseline_dir=str(root / "versions" / second_version / "source_snapshot")
    )
    good = staged(root, good_spec, "current-baseline")
    activate(good)
    assert len(evidence(root)) == len(previous) + 1


def test_malformed_file_and_key_conflict_fail_closed(setup):
    root, spec, incoming, rows = setup
    active = staged(root, spec)
    activate(active)
    previous = evidence(root)
    _write_csv(incoming, KOSHA_COLUMNS, rows[:2])
    with incoming.open("a") as handle:
        handle.write('"unterminated')
    bad = batch.Pipeline(root, "parse-error")
    bad.initialize(spec)
    bad.execute("collect")
    bad.execute("archive")
    with pytest.raises(batch.BatchError, match="FILE_PARSE_ERROR"):
        bad.execute("normalize")
    assert (
        batch.read(bad.work / "exclusions.json")["file_error"]
        == "FILE_PARSE_ERROR_INPUT_COUNT_UNKNOWN"
    )
    assert evidence(root) == previous
    _write_csv(
        incoming, KOSHA_COLUMNS, [dict(rows[0], 상세내용="같은 키 다른 내용"), rows[1]]
    )
    conflict = batch.Pipeline(root, "key-conflict")
    conflict.initialize(spec)
    for step in batch.STEPS[:3]:
        conflict.execute(step)
    with pytest.raises(batch.BatchError, match="BASELINE_RECORD_ID_CONTENT_CONFLICT"):
        conflict.execute("validate")
    assert evidence(root) == previous


def test_active_runtime_pins_bundle_and_existing_release_gate_remains(setup):
    from chemiguard119.api import ModelRuntime
    from chemiguard119.release import RuntimeIntegrityError

    root, spec, _, _ = setup
    p = staged(root, spec)
    activate(p)
    paths = batch.active_runtime_paths(root)
    runtime = ModelRuntime.load(**paths, environment="development")
    assert runtime.db_path == paths["db_path"]
    assert runtime.resolver_model_path.parent == runtime.db_path.parent
    assert runtime.retriever_model_path.parent == runtime.db_path.parent
    with pytest.raises(RuntimeIntegrityError):
        ModelRuntime.load(**paths, environment="production")


def test_simultaneous_activation_is_idempotent(setup):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    root, spec, _, _ = setup
    left = staged(root, spec, "left")
    right = staged(root, spec, "right")
    version = left.state()["version"]
    assert right.state()["version"] == version
    batch.approve(root, version, "test", "synthetic concurrent review")
    barrier = Barrier(2)

    def run(p):
        barrier.wait(timeout=5)
        p.execute("activate")
        return p.finish()

    with ThreadPoolExecutor(max_workers=2) as pool:
        a, b = pool.submit(run, left), pool.submit(run, right)
        assert a.result(timeout=10)["status"] == "SUCCESS"
        assert b.result(timeout=10)["status"] == "SUCCESS"
    assert batch.current(root)["version"] == version
    assert len(evidence(root)) == 4


def test_cli_reprocess_uses_archived_snapshot(setup):
    import json
    import os
    import subprocess
    import sys

    root, spec, incoming, _ = setup
    p = staged(root, spec)
    version = activate(p)
    incoming.unlink()  # replay must not depend on the original input path
    environment = dict(os.environ, PYTHONPATH=str(Path(__file__).parents[1] / "src"))
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "chemiguard119.reference_batch",
            "--root",
            str(root),
            "reprocess",
            "--version",
            version,
            "--run-id",
            "cli-replay",
            "--through",
            "activate",
        ],
        env=environment,
        capture_output=True,
        text=True,
        timeout=20,
    )
    assert result.returncode == 0, result.stderr
    report = json.loads(result.stdout)
    assert report["status"] == "SUCCESS"
    assert report["version"] == version
    assert report["counts"]["stored"] == 2
    assert len(evidence(root)) == 4


def test_blank_and_no_information_rows_block_default_exclusion_threshold(setup):
    root, spec, incoming, rows = setup
    activate(staged(root, spec))
    previous = evidence(root)
    _write_csv(incoming, KOSHA_COLUMNS, rows)
    strict_spec = dict(spec, max_excluded_fraction=0.1)
    p = batch.Pipeline(root, "realistic-empty-detail")
    p.initialize(strict_spec)
    for step in batch.STEPS[:3]:
        p.execute(step)
    assert p.state()["counts"]["input"] == 4
    assert p.state()["counts"]["normal"] == 2
    assert p.state()["counts"]["excluded"] == 2
    assert p.state()["exclusion_reasons"] == {
        "EMPTY_LEAF_DETAIL": 1,
        "NO_INFORMATION": 1,
    }
    with pytest.raises(batch.BatchError, match="EXCLUSION_THRESHOLD_EXCEEDED"):
        p.execute("validate")
    assert evidence(root) == previous


def test_expected_parent_and_no_information_are_reviewed_not_counted_as_errors(setup):
    root, spec, incoming, rows = setup
    from chemiguard119.kosha_client import KOSHA_STAGING_COLUMNS

    source = [
        {
            **{c: "" for c in KOSHA_STAGING_COLUMNS},
            **rows[0],
            "상세내용": "",
            "레코드ID": "parent",
            "MSDS_항목코드": "P",
        },
        {
            **{c: "" for c in KOSHA_STAGING_COLUMNS},
            **rows[0],
            "레코드ID": "child",
            "MSDS_항목코드": "C",
            "상위항목코드": "P",
        },
        {**{c: "" for c in KOSHA_STAGING_COLUMNS}, **rows[1]},
        {
            **{c: "" for c in KOSHA_STAGING_COLUMNS},
            **rows[0],
            "레코드ID": "no-info",
            "상세내용": "자료없음",
        },
    ]
    batch.write_csv(incoming, KOSHA_STAGING_COLUMNS, source)
    spec = dict(spec, max_error_fraction=0.0)
    p = staged(root, spec, "expected-content")
    q = p.state()["quality_summary"]
    assert q["error_excluded"] == 0 and q["expected_excluded"] == 2
    assert p.state()["counts"]["normal"] == 2
    assert p.state()["exclusion_reasons"] == {
        "STRUCTURAL_PARENT_NO_DETAIL": 1,
        "NO_INFORMATION": 1,
    }
    v = p.state()["version"]
    batch.approve(root, v, "fixture reviewer", "synthetic quality test", True)
    with pytest.raises(batch.BatchError, match="EXPECTED_EXCLUSIONS_REVIEW_REQUIRED"):
        p.execute("activate")
    batch.approve(
        root, v, "fixture reviewer", "synthetic expected-content review", True, True
    )
    p.execute("activate")
    assert p.finish()["status"] == "SUCCESS"


def test_invalid_parent_is_still_an_error(setup):
    root, spec, incoming, rows = setup
    from chemiguard119.kosha_client import KOSHA_STAGING_COLUMNS

    source = [
        {
            **{c: "" for c in KOSHA_STAGING_COLUMNS},
            **rows[0],
            "레코드ID": "parent",
            "CAS번호": "",
            "상세내용": "",
            "MSDS_항목코드": "P",
        },
        {
            **{c: "" for c in KOSHA_STAGING_COLUMNS},
            **rows[0],
            "레코드ID": "child",
            "상위항목코드": "P",
        },
        {**{c: "" for c in KOSHA_STAGING_COLUMNS}, **rows[1]},
    ]
    batch.write_csv(incoming, KOSHA_STAGING_COLUMNS, source)
    p = batch.Pipeline(root, "invalid-parent")
    p.initialize(dict(spec, max_error_fraction=0.0))
    for step in batch.STEPS[:3]:
        p.execute(step)
    assert p.state()["quality_summary"]["error_excluded"] == 1
    with pytest.raises(batch.BatchError, match="EXCLUSION_THRESHOLD_EXCEEDED"):
        p.execute("validate")


def test_initialization_rejects_output_nested_in_baseline_without_copy(setup):
    _, spec, _, _ = setup
    baseline = Path(spec["baseline_dir"])
    output = baseline / "nested-batch"
    pipeline = batch.Pipeline(output, "overlap")
    with pytest.raises(batch.BatchError, match="INPUT_OUTPUT_PATH_OVERLAP"):
        pipeline.initialize(spec)
    assert not output.exists()
    assert all(
        (baseline / name).exists() for name in batch.preprocessing.SOURCE_FILES.values()
    )


def test_initialization_does_not_delete_source_in_existing_run_scratch(setup):
    root, spec, _, _ = setup
    pipeline = batch.Pipeline(root, "nested-source")
    nested = pipeline.work / "retained-input"
    shutil.copytree(spec["baseline_dir"], nested)
    spec = {**spec, "baseline_dir": str(nested)}
    before = batch.tree_hashes(nested)
    with pytest.raises(batch.BatchError, match="INPUT_OUTPUT_PATH_OVERLAP"):
        pipeline.initialize(spec)
    assert batch.tree_hashes(nested) == before
